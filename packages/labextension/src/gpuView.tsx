import { ReactWidget } from '@jupyterlab/apputils';

import React, { useState, useEffect, ReactElement } from 'react';

import { IndicatorComponent } from './indicator';

import { ResourceUsage } from './model';

export const DEFAULT_GPU_LABEL = 'GPU: ';

/**
 * A GpuView component to display GPU usage.
 */
const GpuViewComponent = ({
  model,
  label,
}: {
  model: ResourceUsage.Model;
  label: string;
}): ReactElement => {
  const [text, setText] = useState('');
  const [values, setValues] = useState<number[]>([]);

  const update = (): void => {
    const { gpuMemoryLimit, currentGpuMemory } = model;
    const newText = gpuMemoryLimit
      ? `${currentGpuMemory.toFixed(0)} / ${gpuMemoryLimit.toFixed(0)} ${
          model.gpuMemoryUnits
        }`
      : `${currentGpuMemory.toFixed(0)} ${model.gpuMemoryUnits}`;
    const newValues = model.values.map((value) => value.gpuMemoryPercent);
    setText(newText);
    setValues(newValues);
  };

  useEffect(() => {
    model.stateChanged.connect(update);
    return (): void => {
      model.stateChanged.disconnect(update);
    };
  }, [model]);

  return (
    <IndicatorComponent
      enabled={model.gpuMemoryAvailable}
      values={values}
      label={label}
      color={'#00B35B'}
      text={text}
    />
  );
};

export namespace GpuView {
  /**
   * Create a new GpuView React Widget.
   *
   * @param model The resource usage model.
   * @param label The label next to the component.
   */
  export const createGpuView = (
    model: ResourceUsage.Model,
    label: string
  ): ReactWidget => {
    return ReactWidget.create(<GpuViewComponent model={model} label={label} />);
  };
}
